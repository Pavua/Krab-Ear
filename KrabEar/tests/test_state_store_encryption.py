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

import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
    """A5: encrypt_line упал → отказ записи и громкий error_bus push."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self.data_dir = Path(self._tmpdir)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_encrypt_failure_pushes_error_and_rejects_plaintext(self) -> None:
        from unittest.mock import MagicMock
        from backend.state_store import HistoryEncryptionUnavailable, StateStore

        store = StateStore(self.data_dir)
        # Крипто, чей encrypt_line всегда падает (имитация native-сбоя AESGCM).
        fake_crypto = MagicMock()
        fake_crypto.encrypt_line.side_effect = RuntimeError("AESGCM boom")
        store._history_crypto_initialized = True
        store._history_crypto_instance = fake_crypto
        # Подключаем фейковый error_bus, чтобы поймать громкий push.
        bus = MagicMock()
        store._error_bus = bus

        with self.assertRaises(HistoryEncryptionUnavailable):
            store._maybe_encrypt('{"id":"x","text":"secret"}')

        # Ошибка сохраняется локально для retry, но на диск plaintext не уходит.
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
        store.initialize_startup_plaintext_policy(new_profile=True)
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
        store.initialize_startup_plaintext_policy(new_profile=True)
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
        store.initialize_startup_plaintext_policy(new_profile=True)
        store.save_settings({"history_encryption_enabled": True})

        store2 = StateStore(self.data_dir)
        store2._history_crypto_initialized = True
        store2._history_crypto_instance = self._fake_crypto

        settings = store2.load_settings()
        self.assertTrue(settings["history_encryption_enabled"])


class TestStateStorePassiveKeyAccess(unittest.TestCase):
    """Пассивные пути не создают замену пропавшему ключу истории."""

    def setUp(self) -> None:
        from backend.history_crypto import HistoryCrypto
        from backend.state_store import StateStore

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)
        self.old_key = os.urandom(32)
        self.old_crypto = HistoryCrypto(self.old_key)
        self.key = None
        self.commands = []
        self.store = StateStore(self.data_dir)
        self.store.settings_path.write_text(json.dumps({
            "history_encryption_enabled": True,
            "privacy_mode_enabled": False,
            "settings_revision": "0" * 32,
        }), encoding="utf-8")
        self.store.lock_path.touch(exist_ok=True)
        provider = patch("backend.crypto_keystore._run_security", side_effect=self.security)
        provider.start()
        self.addCleanup(provider.stop)

    def security(self, args):
        """Настоящий CLI seam: missing=44, ошибочное создание УСПЕШНО."""
        self.commands.append(tuple(args))
        if args[0] == "find-generic-password":
            if self.key is None:
                return subprocess.CompletedProcess(args, 44, "", "not found")
            return subprocess.CompletedProcess(
                args, 0, base64.b64encode(self.key).decode("ascii") + "\n", ""
            )
        if args[0] == "add-generic-password":
            self.key = base64.b64decode(args[args.index("-w") + 1])
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError("unexpected security operation")

    def profile_bytes(self):
        return {
            str(path.relative_to(self.data_dir)): path.read_bytes()
            for path in self.data_dir.rglob("*") if path.is_file()
        }

    def assert_no_create(self):
        self.assertFalse(
            any(args[0] == "add-generic-password" for args in self.commands),
            "пассивный доступ вызвал создание нового ключа",
        )

    def write_history(self, encrypted=True):
        payload = json.dumps(_make_history_item("old-item", "Синтетическая запись").to_dict())
        self.store.history_path.write_text(
            (self.old_crypto.encrypt_line(payload) if encrypted else payload) + "\n",
            encoding="utf-8",
        )

    def test_missing_key_read_does_not_create_and_same_store_recovers(self):
        from backend.state_store import HistoryEncryptionUnavailable

        self.write_history()
        before = self.profile_bytes()
        with self.assertRaises(HistoryEncryptionUnavailable):
            self.store.get_history_page(None, 100)
        self.assert_no_create()
        self.assertEqual(self.profile_bytes(), before)
        self.key = self.old_key
        items, _ = self.store.get_history_page(None, 100)
        self.assertEqual([item["id"] for item in items], ["old-item"])
        self.assert_no_create()

    def test_sidecar_only_missing_key_does_not_create_or_rewrite(self):
        from backend.state_store import HistoryEncryptionUnavailable

        self.write_history(encrypted=False)
        self.store.status_path.write_text(self.old_crypto.encrypt_line(json.dumps({
            "id": "old-item", "paste_status": "ok",
        })) + "\n", encoding="utf-8")
        before = self.profile_bytes()
        with self.assertRaises(HistoryEncryptionUnavailable):
            self.store.get_history_page(None, 100)
        self.assert_no_create()
        self.assertEqual(self.profile_bytes(), before)
        self.key = self.old_key
        items, _ = self.store.get_history_page(None, 100)
        self.assertEqual(items[0]["paste_status"], "ok")

    def test_default_getter_retries_missing_key_on_same_store(self):
        before = self.profile_bytes()
        crypto = self.store._get_history_crypto()
        self.assert_no_create()
        self.assertIsNone(crypto)
        self.assertEqual(self.profile_bytes(), before)
        self.key = self.old_key
        crypto = self.store._get_history_crypto()
        self.assertIsNotNone(crypto)
        token = self.old_crypto.encrypt_line("synthetic")
        self.assertEqual(crypto.decrypt_line(token), "synthetic")

    def test_empty_on_snapshot_getter_is_passive_and_retries(self):
        from backend.history_service import HistoryService

        service = object.__new__(HistoryService)
        service.store = self.store
        before = self.profile_bytes()
        crypto = service._history_crypto_for_snapshot()
        self.assert_no_create()
        self.assertIsNone(crypto)
        self.assertEqual(self.profile_bytes(), before)
        self.key = self.old_key
        self.assertIsNotNone(service._history_crypto_for_snapshot())
        self.assert_no_create()

    def test_pending_recovery_missing_key_preserves_profile(self):
        from backend.encrypted_snapshot import (
            REASON_CRYPTO_UNAVAILABLE, recover_pending_restore_from_store,
        )

        self.write_history()
        staging = self.data_dir / ".a52b2-restore-synthetic"
        staging.mkdir()
        (staging / "restore_marker.json").write_text(json.dumps({
            "version": 1, "state": "COMMITTING", "transaction_id": "synthetic",
            "target_snapshot": "missing-target", "pre_restore_snapshot": None,
        }), encoding="utf-8")
        before = self.profile_bytes()
        result = recover_pending_restore_from_store(self.store)
        self.assert_no_create()
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], REASON_CRYPTO_UNAVAILABLE)
        self.assertEqual(self.profile_bytes(), before)

    def test_existing_key_loads_without_create(self):
        self.write_history()
        self.key = self.old_key
        items, _ = self.store.get_history_page(None, 100)
        self.assertEqual(items[0]["id"], "old-item")
        self.assert_no_create()

    def test_wrong_key_rejects_read_without_create_or_rewrite(self):
        from backend.state_store import HistoryEncryptionUnavailable

        self.write_history()
        self.key = os.urandom(32)
        before = self.profile_bytes()
        with self.assertRaises(HistoryEncryptionUnavailable):
            self.store.get_history_page(None, 100)
        self.assert_no_create()
        self.assertEqual(self.profile_bytes(), before)

    def test_explicit_new_write_can_create_after_passive_miss(self):
        from backend.state_store import StateStore

        self.store._get_history_crypto()
        self.commands.clear()
        item = _add_item(self.store, "Новая синтетическая запись")
        self.assertEqual(sum(args[0] == "add-generic-password" for args in self.commands), 1)
        items, _ = StateStore(self.data_dir).get_history_page(None, 100)
        self.assertEqual(items[0]["id"], item.id)

    def test_direct_migration_preserves_deliberate_creation(self):
        self.write_history(encrypted=False)
        result = self.store.migrate_history_encryption()
        self.assertTrue(result["ok"])
        self.assertEqual(sum(args[0] == "add-generic-password" for args in self.commands), 1)
        items, _ = self.store.get_history_page(None, 100)
        self.assertEqual(items[0]["id"], "old-item")


if __name__ == "__main__":
    unittest.main()
