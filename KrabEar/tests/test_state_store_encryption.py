"""Тесты интеграции шифрования StateStore.

Проверяют:
- default off → строки plaintext JSON (байт-идентично текущему поведению)
- encryption enabled → строки в файле начинаются с ENC1:
- read back encrypted file → возвращает исходные HistoryItem
- mixed file (plaintext + encrypted) → читает оба корректно
- tombstone encryption → удалённые id шифруются/расшифровываются
- settings.json ВСЕГДА остаётся plaintext
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _add_item(store, text: str):
    """Добавляет HistoryItem через реальную сигнатуру add_history_item."""
    return store.add_history_item(text=text)


def _make_history_item(item_id: str, text: str):
    """Создаёт минимальный HistoryItem для прямой записи в файл."""
    from backend.models import HistoryItem
    return HistoryItem(id=item_id, ts="2026-01-01T00:00:00Z", text=text)


class TestStateStoreEncryptFailureLoud(unittest.TestCase):
    """Crypto-audit (2026-06-20): encrypt_line упал → plaintext fallback (данные
    не теряем), НО громкий error_bus push (не молчаливая security-регрессия)."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self.data_dir = Path(self._tmpdir)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_encrypt_failure_pushes_error_and_falls_back_to_plaintext(self) -> None:
        from unittest.mock import MagicMock
        from backend.state_store import StateStore

        store = StateStore(self.data_dir)
        # Крипто, чей encrypt_line всегда падает (имитация native-сбоя AESGCM).
        fake_crypto = MagicMock()
        fake_crypto.encrypt_line.side_effect = RuntimeError("AESGCM boom")
        store._history_crypto_initialized = True
        store._history_crypto_instance = fake_crypto
        # Подключаем фейковый error_bus, чтобы поймать громкий push.
        bus = MagicMock()
        store._error_bus = bus

        out = store._maybe_encrypt('{"id":"x","text":"secret"}')

        # 1. Данные не потеряны — вернулся plaintext.
        self.assertEqual(out, '{"id":"x","text":"secret"}')
        # 2. Громко: error_bus.push вызван (history.encrypt_fail).
        self.assertTrue(bus.push.called, "ожидался громкий error_bus push при сбое шифрования")


class TestStateStoreEncryptionDefaultOff(unittest.TestCase):
    """По умолчанию шифрование выключено — файл содержит plaintext JSON."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self.data_dir = Path(self._tmpdir)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_history_file_is_plaintext_by_default(self) -> None:
        from backend.state_store import StateStore

        store = StateStore(self.data_dir)
        # default: _history_crypto_initialized=False → _get_history_crypto()
        # will call load_settings → history_encryption_enabled is False by default
        # → no crypto → plaintext

        _add_item(store, "Тест")
        raw = self.data_dir.joinpath("history.ndjson").read_text(encoding="utf-8").strip()
        self.assertFalse(raw.startswith("ENC1:"))
        payload = json.loads(raw)
        self.assertEqual(payload["text"], "Тест")

    def test_settings_file_always_plaintext_even_when_crypto_injected(self) -> None:
        """settings.json должен оставаться plaintext ВСЕГДА."""
        from backend.history_crypto import HistoryCrypto
        from backend.state_store import StateStore

        store = StateStore(self.data_dir)
        store._history_crypto_initialized = True
        store._history_crypto_instance = HistoryCrypto(os.urandom(32))

        store.save_settings({"history_encryption_enabled": True})
        raw = self.data_dir.joinpath("settings.json").read_text(encoding="utf-8")
        parsed = json.loads(raw)
        self.assertIn("history_encryption_enabled", parsed)
        self.assertFalse(raw.strip().startswith("ENC1:"))


class TestStateStoreEncryptionEnabled(unittest.TestCase):
    """С шифрованием включённым: файл содержит ENC1:-строки, read back работает."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self.data_dir = Path(self._tmpdir)
        self._key = os.urandom(32)

        from backend.history_crypto import HistoryCrypto
        self._fake_crypto = HistoryCrypto(self._key)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _make_encrypted_store(self):
        """StateStore с шифрованием через наш ключ (Keychain обходится)."""
        from backend.state_store import StateStore

        store = StateStore(self.data_dir)
        store._history_crypto_initialized = True
        store._history_crypto_instance = self._fake_crypto
        return store

    def test_history_lines_encrypted(self) -> None:
        store = self._make_encrypted_store()
        _add_item(store, "Секрет")

        raw = self.data_dir.joinpath("history.ndjson").read_text(encoding="utf-8").strip()
        self.assertTrue(raw.startswith("ENC1:"), f"Ожидался ENC1: но получено: {raw[:40]}")

    def test_read_back_encrypted_returns_original_item(self) -> None:
        store = self._make_encrypted_store()
        item = _add_item(store, "Секрет пользователя")

        store2 = self._make_encrypted_store()
        items, _ = store2.get_history_page(None, 100)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["text"], "Секрет пользователя")
        self.assertEqual(items[0]["id"], item.id)

    def test_multiple_items_encrypted_and_readable(self) -> None:
        store = self._make_encrypted_store()
        for i in range(5):
            _add_item(store, f"Текст {i}")

        store2 = self._make_encrypted_store()
        items, _ = store2.get_history_page(None, 100)
        self.assertEqual(len(items), 5)
        texts = {item["text"] for item in items}
        for i in range(5):
            self.assertIn(f"Текст {i}", texts)


class TestStateStoreMixedFile(unittest.TestCase):
    """Смешанный файл: plaintext + encrypted строки читаются корректно."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self.data_dir = Path(self._tmpdir)
        self._key = os.urandom(32)

        from backend.history_crypto import HistoryCrypto
        self._fake_crypto = HistoryCrypto(self._key)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_mixed_plaintext_and_encrypted_lines(self) -> None:
        from backend.state_store import StateStore

        # Записываем одну plaintext строку напрямую
        pt_item = _make_history_item("plain-001", "Plaintext item")
        history_file = self.data_dir.joinpath("history.ndjson")
        with history_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(pt_item.to_dict(), ensure_ascii=False) + "\n")

        # Добавляем зашифрованную строку через store с crypto
        store = StateStore(self.data_dir)
        store._history_crypto_initialized = True
        store._history_crypto_instance = self._fake_crypto
        _add_item(store, "Encrypted item")

        # Читаем через store с crypto — должны получить обе записи
        store2 = StateStore(self.data_dir)
        store2._history_crypto_initialized = True
        store2._history_crypto_instance = self._fake_crypto

        items, _ = store2.get_history_page(None, 100)
        texts = {item["text"] for item in items}
        self.assertIn("Plaintext item", texts)
        self.assertIn("Encrypted item", texts)
        self.assertEqual(len(items), 2)

    def test_history_file_contains_both_enc_and_plain_lines(self) -> None:
        from backend.state_store import StateStore

        # Plaintext строка
        pt_item = _make_history_item("plain-002", "Plain")
        history_file = self.data_dir.joinpath("history.ndjson")
        with history_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(pt_item.to_dict(), ensure_ascii=False) + "\n")

        # Зашифрованная строка
        store = StateStore(self.data_dir)
        store._history_crypto_initialized = True
        store._history_crypto_instance = self._fake_crypto
        _add_item(store, "Secret")

        lines = history_file.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)
        enc_lines = [ln for ln in lines if ln.startswith("ENC1:")]
        plain_lines = [ln for ln in lines if not ln.startswith("ENC1:")]
        self.assertEqual(len(enc_lines), 1)
        self.assertEqual(len(plain_lines), 1)


class TestStateStoreTombstoneEncryption(unittest.TestCase):
    """Tombstone записи шифруются и читаются корректно."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self.data_dir = Path(self._tmpdir)
        self._key = os.urandom(32)

        from backend.history_crypto import HistoryCrypto
        self._fake_crypto = HistoryCrypto(self._key)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _make_store(self):
        from backend.state_store import StateStore
        store = StateStore(self.data_dir)
        store._history_crypto_initialized = True
        store._history_crypto_instance = self._fake_crypto
        return store

    def test_tombstone_file_encrypted(self) -> None:
        store = self._make_store()
        item = _add_item(store, "To delete")
        store.delete_history_item(item.id)

        tombstone_file = self.data_dir.joinpath("history_tombstones.ndjson")
        raw = tombstone_file.read_text(encoding="utf-8").strip()
        self.assertTrue(raw.startswith("ENC1:"))

    def test_deleted_item_not_returned(self) -> None:
        store = self._make_store()
        item = _add_item(store, "Delete me")
        store.delete_history_item(item.id)

        store2 = self._make_store()
        items, _ = store2.get_history_page(None, 100)
        ids = [i["id"] for i in items]
        self.assertNotIn(item.id, ids)

    def test_deletion_with_crypto_off_reads_tombstone_correctly(self) -> None:
        """Plaintext tombstone читается корректно когда crypto выключен."""
        from backend.state_store import StateStore

        # Store без crypto — tombstone plaintext
        store_plain = StateStore(self.data_dir)
        store_plain._history_crypto_initialized = True
        store_plain._history_crypto_instance = None
        item = _add_item(store_plain, "Plain tombstone test")
        store_plain.delete_history_item(item.id)

        tombstone_file = self.data_dir.joinpath("history_tombstones.ndjson")
        raw = tombstone_file.read_text(encoding="utf-8").strip()
        self.assertFalse(raw.startswith("ENC1:"))

        # Читаем через store с crypto — plaintext tombstone всё равно применяется
        store_enc = StateStore(self.data_dir)
        store_enc._history_crypto_initialized = True
        store_enc._history_crypto_instance = self._fake_crypto
        items, _ = store_enc.get_history_page(None, 100)
        self.assertEqual(len(items), 0)


class TestStateStoreSettingsAlwaysPlaintext(unittest.TestCase):
    """settings.json НИКОГДА не шифруется."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self.data_dir = Path(self._tmpdir)
        self._key = os.urandom(32)

        from backend.history_crypto import HistoryCrypto
        self._fake_crypto = HistoryCrypto(self._key)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_save_settings_is_plaintext_json(self) -> None:
        from backend.state_store import StateStore

        store = StateStore(self.data_dir)
        store._history_crypto_initialized = True
        store._history_crypto_instance = self._fake_crypto

        store.save_settings({"history_encryption_enabled": True})
        raw = self.data_dir.joinpath("settings.json").read_text(encoding="utf-8")
        self.assertFalse(raw.strip().startswith("ENC1:"))
        parsed = json.loads(raw)
        self.assertEqual(parsed["history_encryption_enabled"], True)

    def test_load_settings_not_affected_by_crypto(self) -> None:
        from backend.state_store import StateStore

        store = StateStore(self.data_dir)
        store.save_settings({"history_encryption_enabled": True})

        store2 = StateStore(self.data_dir)
        store2._history_crypto_initialized = True
        store2._history_crypto_instance = self._fake_crypto

        settings = store2.load_settings()
        self.assertTrue(settings["history_encryption_enabled"])


if __name__ == "__main__":
    unittest.main()
