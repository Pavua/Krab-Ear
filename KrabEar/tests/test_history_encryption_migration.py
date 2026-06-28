"""Тесты at-rest encryption migration для StateStore и IPC-обработчиков.

Полностью изолированы от macOS Keychain: inject FakeHistoryCrypto напрямую
в store._history_crypto_instance — Keychain в CI (ubuntu) не нужен.

Покрытие:
- migrate_history_encryption шифрует plaintext-строки
- ENC1:-строки проходят неизменными (идемпотентность частичная)
- round-trip integrity: item-ы до и после идентичны
- полная идемпотентность: 2-й запуск шифрует 0 строк
- .bak файл создаётся до замены
- атомарность: tmp-файл удаляется при ошибке записи (live-файл не тронут)
- crypto-unavailable → graceful no-op {ok: False, reason: "encryption_unavailable"}
- tombstone/структурные строки сохраняются
- progress_cb вызывается во время миграции
- get_history_encryption_status: корректно считает ENC1: vs plaintext
- IPC migrate_history_encryption: status "started" / "encryption_unavailable"
- IPC get_history_encryption_status: поля ok/enabled/total/encrypted/plaintext/pct/migrating
- BackendService.close() в tearDown (🔴 daemon-thread rule)
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_crypto():
    """Реальный HistoryCrypto с рандомным ключом (не требует Keychain)."""
    from backend.history_crypto import HistoryCrypto
    return HistoryCrypto(os.urandom(32))


def _inject_crypto(store, crypto=None):
    """Подменяет крипто-инстанс в StateStore (обход Keychain)."""
    if crypto is None:
        crypto = _make_fake_crypto()
    store._history_crypto_initialized = True
    store._history_crypto_instance = crypto
    return crypto


def _make_store(data_dir: Path):
    from backend.state_store import StateStore
    return StateStore(data_dir)


def _write_plaintext_line(history_path: Path, payload: dict) -> None:
    """Дописывает plaintext JSON-строку напрямую в history.ndjson."""
    with history_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _write_enc1_line(history_path: Path, payload: dict, crypto) -> None:
    """Дописывает уже зашифрованную строку в history.ndjson."""
    from backend.history_crypto import HistoryCrypto  # noqa: F401
    line = crypto.encrypt_line(json.dumps(payload, ensure_ascii=False))
    with history_path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _read_all_raw_lines(history_path: Path) -> list[str]:
    """Читает все непустые строки из файла без расшифровки."""
    lines = []
    with history_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            s = line.strip()
            if s:
                lines.append(s)
    return lines


def _count_enc1(history_path: Path) -> int:
    from backend.history_crypto import HistoryCrypto
    count = 0
    for line in _read_all_raw_lines(history_path):
        if HistoryCrypto.is_encrypted(line):
            count += 1
    return count


def _make_item_payload(item_id: str, text: str) -> dict:
    return {
        "id": item_id,
        "ts": "2026-01-01T00:00:00Z",
        "text": text,
        "confidence": 0.9,
        "duration": 5.0,
    }


# ---------------------------------------------------------------------------
# StateStore.migrate_history_encryption  — data-safety tests
# ---------------------------------------------------------------------------

class TestMigrateEncryptsPlaintext(unittest.TestCase):
    """Миграция шифрует plaintext-строки."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.data_dir = Path(self._tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_plaintext_lines_become_enc1(self):
        store = _make_store(self.data_dir)
        _inject_crypto(store)

        _write_plaintext_line(self.data_dir / "history.ndjson", _make_item_payload("id1", "Привет"))
        _write_plaintext_line(self.data_dir / "history.ndjson", _make_item_payload("id2", "Мир"))

        # Force re-init with injected crypto for migration too
        result = store.migrate_history_encryption()

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["encrypted"], 2)
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["already_encrypted"], 0)

        # Все строки в файле теперь ENC1:
        self.assertEqual(_count_enc1(self.data_dir / "history.ndjson"), 2)

    def test_enc1_lines_pass_through_unchanged(self):
        """Уже зашифрованные строки не перешифровываются."""
        store = _make_store(self.data_dir)
        crypto = _inject_crypto(store)

        # Уже зашифрованная строка
        _write_enc1_line(self.data_dir / "history.ndjson", _make_item_payload("id1", "Секрет"), crypto)

        result = store.migrate_history_encryption()

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["encrypted"], 0, "Уже зашифрованные строки не должны перешифровываться")
        self.assertEqual(result["already_encrypted"], 1)

    def test_mixed_file_partial_encryption(self):
        """Смешанный файл: только plaintext шифруется, ENC1: не трогается."""
        store = _make_store(self.data_dir)
        crypto = _inject_crypto(store)

        _write_enc1_line(self.data_dir / "history.ndjson", _make_item_payload("id1", "Зашифрован"), crypto)
        _write_plaintext_line(self.data_dir / "history.ndjson", _make_item_payload("id2", "Открытый"))

        result = store.migrate_history_encryption()

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["encrypted"], 1)
        self.assertEqual(result["already_encrypted"], 1)
        self.assertEqual(_count_enc1(self.data_dir / "history.ndjson"), 2)


class TestMigrateRoundTripIntegrity(unittest.TestCase):
    """Round-trip: логические items идентичны до и после миграции."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.data_dir = Path(self._tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_items_identical_before_and_after_migration(self):
        store = _make_store(self.data_dir)
        crypto = _inject_crypto(store)

        # Добавляем несколько разных записей
        payloads = [
            _make_item_payload("id1", "Первый текст"),
            _make_item_payload("id2", "Second text"),
            _make_item_payload("id3", "Терcer texto"),
        ]
        for p in payloads:
            _write_plaintext_line(self.data_dir / "history.ndjson", p)

        # Читаем items ДО миграции (через _read_history_ndjson_unlocked)
        # Крипто не инициализирован для расшифровки → читаем plaintext как есть
        store2 = _make_store(self.data_dir)
        _inject_crypto(store2, crypto)
        # Отключаем крипто для pre-migration чтения
        store_pre = _make_store(self.data_dir)
        store_pre._history_crypto_initialized = True
        store_pre._history_crypto_instance = None  # plaintext mode
        items_before = []
        with store_pre._lock():
            for item in store_pre._iter_history_items_unlocked():
                items_before.append(item.to_dict())

        # Миграция
        store_migrate = _make_store(self.data_dir)
        _inject_crypto(store_migrate, crypto)
        result = store_migrate.migrate_history_encryption()
        self.assertTrue(result["ok"])

        # Читаем items ПОСЛЕ миграции (с расшифровкой)
        store_post = _make_store(self.data_dir)
        _inject_crypto(store_post, crypto)
        items_after = []
        with store_post._lock():
            for item in store_post._iter_history_items_unlocked():
                items_after.append(item.to_dict())

        # Логические данные идентичны
        self.assertEqual(len(items_before), len(items_after), "Количество items должно совпасть")
        ids_before = {i["id"] for i in items_before}
        ids_after = {i["id"] for i in items_after}
        self.assertEqual(ids_before, ids_after, "ID items должны совпасть")

        texts_before = {i["id"]: i["text"] for i in items_before}
        texts_after = {i["id"]: i["text"] for i in items_after}
        self.assertEqual(texts_before, texts_after, "Тексты items должны совпасть")


class TestMigrateIdempotent(unittest.TestCase):
    """Повторный запуск миграции шифрует 0 новых строк."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.data_dir = Path(self._tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_second_run_encrypts_zero(self):
        store = _make_store(self.data_dir)
        crypto = _inject_crypto(store)

        _write_plaintext_line(self.data_dir / "history.ndjson", _make_item_payload("id1", "Текст"))

        # Первый запуск
        r1 = store.migrate_history_encryption()
        self.assertTrue(r1["ok"])
        self.assertEqual(r1["encrypted"], 1)

        # Второй запуск — тот же store (crypto уже инициализирован)
        store2 = _make_store(self.data_dir)
        _inject_crypto(store2, crypto)
        r2 = store2.migrate_history_encryption()
        self.assertTrue(r2["ok"])
        self.assertEqual(r2["encrypted"], 0, "Повторная миграция не должна шифровать уже зашифрованные строки")
        self.assertEqual(r2["already_encrypted"], 1)


class TestMigrateBakCreated(unittest.TestCase):
    """.bak файл создаётся перед атомарной заменой, затем безопасно удаляется."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.data_dir = Path(self._tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_bak_file_removed_after_successful_migration(self):
        """После успешной миграции .bak должен быть безопасно удалён.

        Это регрессионный тест на баг: ранее .bak оставался на диске навсегда,
        что сводило на нет смысл шифрования — plaintext лежал рядом с зашифрованным
        файлом истории.
        """
        store = _make_store(self.data_dir)
        _inject_crypto(store)

        secret_text = "Секретные данные пользователя"
        _write_plaintext_line(self.data_dir / "history.ndjson", _make_item_payload("id1", secret_text))

        result = store.migrate_history_encryption()
        self.assertTrue(result["ok"], result)

        bak_path = self.data_dir / "history.ndjson.bak"
        # После успешной миграции .bak должен быть удалён (ключевая проверка)
        self.assertFalse(
            bak_path.exists(),
            ".bak с plaintext-данными НЕ должен оставаться на диске после успешного шифрования",
        )

    def test_bak_plaintext_not_readable_after_migration(self):
        """Ни один файл .bak не должен содержать plaintext транскрипт после миграции."""
        store = _make_store(self.data_dir)
        _inject_crypto(store)

        secret_text = "Конфиденциальный транскрипт"
        _write_plaintext_line(self.data_dir / "history.ndjson", _make_item_payload("id1", secret_text))

        result = store.migrate_history_encryption()
        self.assertTrue(result["ok"], result)

        bak_path = self.data_dir / "history.ndjson.bak"
        if bak_path.exists():
            # Если .bak не удалось удалить (напр. wipe_exc) — убеждаемся,
            # что он не содержит plaintext транскрипт в читаемом виде.
            bak_content = bak_path.read_bytes()
            self.assertNotIn(
                secret_text.encode("utf-8"),
                bak_content,
                ".bak не должен содержать plaintext транскрипт даже если не удалось удалить",
            )


class TestMigrateBakPlaintextNotSurviving(unittest.TestCase):
    """Регрессия wave-4: plaintext .bak не должен пережить успешную миграцию.

    Баг: migrate_history_encryption делал shutil.copy2(history.ndjson → .bak)
    ДО шифрования, затем never удалял .bak.  В итоге полный plaintext-дамп
    лежал рядом с зашифрованным файлом, полностью сводя на нет encryption-at-rest.

    Фикс: после успешного os.replace и self-verify — безопасный wipe+unlink .bak.
    """

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.data_dir = Path(self._tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_bak_does_not_exist_after_successful_migration(self):
        """Ключевой тест: .bak не существует после успешной миграции (fail-before / pass-after)."""
        store = _make_store(self.data_dir)
        fixed_key = b"\x00" * 32
        from backend.history_crypto import HistoryCrypto
        crypto = HistoryCrypto(fixed_key)
        _inject_crypto(store, crypto)

        # Seed N plaintext items
        n = 6
        for i in range(n):
            _write_plaintext_line(
                self.data_dir / "history.ndjson",
                _make_item_payload(f"id{i}", f"Транскрипт {i}"),
            )

        result = store.migrate_history_encryption()

        # (a) migration ok=True, encrypted==N
        self.assertTrue(result["ok"], f"Ожидался ok=True, получено: {result}")
        self.assertEqual(result["encrypted"], n)
        self.assertEqual(result["total"], n)

        # (b) history.ndjson is all-ENC1
        raw_lines = _read_all_raw_lines(self.data_dir / "history.ndjson")
        self.assertEqual(len(raw_lines), n)
        for line in raw_lines:
            self.assertTrue(
                HistoryCrypto.is_encrypted(line),
                f"Все строки должны быть ENC1: после миграции, получено: {line[:40]}",
            )

        # (c) round-trip integrity: read back items are byte-identical to pre-migration baseline
        store_post = _make_store(self.data_dir)
        _inject_crypto(store_post, crypto)
        items_after = []
        with store_post._lock():
            for item in store_post._iter_history_items_unlocked():
                items_after.append(item.to_dict())
        self.assertEqual(len(items_after), n, "Количество items после миграции должно совпасть")
        texts_after = {item["id"]: item["text"] for item in items_after}
        for i in range(n):
            self.assertEqual(
                texts_after.get(f"id{i}"),
                f"Транскрипт {i}",
                f"Текст item id{i} должен совпасть после round-trip",
            )

        # (d) .bak no longer exists — КЛЮЧЕВАЯ ПРОВЕРКА (fails before fix, passes after)
        bak_path = self.data_dir / "history.ndjson.bak"
        self.assertFalse(
            bak_path.exists(),
            "history.ndjson.bak с plaintext ДОЛЖЕН быть удалён после успешного шифрования",
        )

    def test_idempotency_second_migration_encrypts_zero(self):
        """(e) Идемпотентность: второй запуск шифрует 0 строк."""
        store = _make_store(self.data_dir)
        fixed_key = b"\x42" * 32
        from backend.history_crypto import HistoryCrypto
        crypto = HistoryCrypto(fixed_key)
        _inject_crypto(store, crypto)

        _write_plaintext_line(
            self.data_dir / "history.ndjson",
            _make_item_payload("id1", "Текст"),
        )

        r1 = store.migrate_history_encryption()
        self.assertTrue(r1["ok"])
        self.assertEqual(r1["encrypted"], 1)

        # Second run
        store2 = _make_store(self.data_dir)
        _inject_crypto(store2, crypto)
        r2 = store2.migrate_history_encryption()
        self.assertTrue(r2["ok"])
        self.assertEqual(r2["encrypted"], 0, "Второй запуск не должен шифровать уже зашифрованные строки")
        self.assertEqual(r2["already_encrypted"], 1)

        # .bak не должен существовать после обоих запусков
        bak_path = self.data_dir / "history.ndjson.bak"
        self.assertFalse(bak_path.exists(), ".bak не должен существовать после повторной миграции")

    def test_rollback_on_verification_failure(self):
        """При сбое self-verify — .bak восстанавливается как live-файл."""
        store = _make_store(self.data_dir)
        fixed_key = b"\x11" * 32
        from backend.history_crypto import HistoryCrypto
        crypto = HistoryCrypto(fixed_key)
        _inject_crypto(store, crypto)

        original_text = "Оригинальный plaintext"
        _write_plaintext_line(
            self.data_dir / "history.ndjson",
            _make_item_payload("id1", original_text),
        )
        original_content = (self.data_dir / "history.ndjson").read_text(encoding="utf-8")

        # Patch decrypt_line to raise → verification fails
        with patch.object(crypto, "decrypt_line", side_effect=Exception("ключ повреждён")):
            result = store.migrate_history_encryption()

        self.assertFalse(result["ok"], "При сбое verification должен вернуться ok=False")
        self.assertEqual(result["reason"], "verification_failed")

        # Live file должен быть восстановлен из .bak (plaintext original)
        restored_content = (self.data_dir / "history.ndjson").read_text(encoding="utf-8")
        self.assertEqual(
            restored_content,
            original_content,
            "Live-файл должен быть восстановлен из .bak при сбое verification",
        )


class TestMigrateAtomicOnError(unittest.TestCase):
    """При ошибке записи tmp — live-файл остаётся нетронутым."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.data_dir = Path(self._tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_live_file_unchanged_on_write_error(self):
        """Симулируем ошибку при финальном os.replace."""
        store = _make_store(self.data_dir)
        _inject_crypto(store)

        original_payload = _make_item_payload("id1", "Оригинальный текст")
        _write_plaintext_line(self.data_dir / "history.ndjson", original_payload)
        original_content = (self.data_dir / "history.ndjson").read_text(encoding="utf-8")

        # Патчим os.replace чтобы он бросал исключение
        with patch("os.replace", side_effect=OSError("диск полон")):
            with self.assertRaises(OSError):
                store.migrate_history_encryption()

        # live-файл не изменился
        after_content = (self.data_dir / "history.ndjson").read_text(encoding="utf-8")
        self.assertEqual(original_content, after_content, "Live-файл должен остаться нетронутым при ошибке")

        # tmp-файл удалён (cleanup)
        tmp_path = self.data_dir / "history.ndjson.migration_tmp"
        self.assertFalse(tmp_path.exists(), "tmp-файл должен быть удалён при ошибке")


class TestMigrateCryptoUnavailable(unittest.TestCase):
    """Если crypto недоступен — graceful no-op."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.data_dir = Path(self._tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_returns_encryption_unavailable_when_crypto_none(self):
        store = _make_store(self.data_dir)
        store._history_crypto_initialized = True
        store._history_crypto_instance = None

        _write_plaintext_line(self.data_dir / "history.ndjson", _make_item_payload("id1", "Данные"))

        result = store.migrate_history_encryption()

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "encryption_unavailable")

        # Файл не изменился (no-op)
        raw = _read_all_raw_lines(self.data_dir / "history.ndjson")
        self.assertEqual(len(raw), 1)
        self.assertFalse(raw[0].startswith("ENC1:"), "Файл не должен быть изменён при недоступном crypto")


class TestMigrateTombstonesPreserved(unittest.TestCase):
    """Структурные строки (tombstone-like, неизвестные форматы) сохраняются."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.data_dir = Path(self._tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_all_lines_preserved_in_order(self):
        """Общее количество строк не должно измениться после миграции."""
        store = _make_store(self.data_dir)
        _inject_crypto(store)

        # Пишем разные типы строк
        lines_before = []
        for i in range(3):
            payload = _make_item_payload(f"id{i}", f"Текст {i}")
            lines_before.append(json.dumps(payload, ensure_ascii=False))
            _write_plaintext_line(self.data_dir / "history.ndjson", payload)
        # Tombstone-подобная строка (delete-марка)
        tombstone = {"type": "delete", "id": "id0", "ts": "2026-01-01T01:00:00Z"}
        lines_before.append(json.dumps(tombstone, ensure_ascii=False))
        _write_plaintext_line(self.data_dir / "history.ndjson", tombstone)

        total_before = len(lines_before)

        result = store.migrate_history_encryption()
        self.assertTrue(result["ok"])
        self.assertEqual(result["total"], total_before, "Количество строк должно сохраниться")

        raw_after = _read_all_raw_lines(self.data_dir / "history.ndjson")
        self.assertEqual(len(raw_after), total_before, "Строки не должны пропасть")
        # Все строки теперь зашифрованы
        from backend.history_crypto import HistoryCrypto
        for line in raw_after:
            self.assertTrue(HistoryCrypto.is_encrypted(line), f"Строка должна быть ENC1:: {line[:30]}")


class TestMigrateProgressCallback(unittest.TestCase):
    """progress_cb вызывается во время миграции."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.data_dir = Path(self._tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_progress_cb_called(self):
        store = _make_store(self.data_dir)
        _inject_crypto(store)

        for i in range(5):
            _write_plaintext_line(self.data_dir / "history.ndjson", _make_item_payload(f"id{i}", f"T{i}"))

        calls: list[tuple] = []

        def cb(total, done, encrypted, pct, status):
            calls.append((total, done, encrypted, pct, status))

        result = store.migrate_history_encryption(progress_cb=cb)
        self.assertTrue(result["ok"])
        self.assertGreater(len(calls), 0, "progress_cb должен быть вызван хотя бы раз")

        # Финальный вызов должен иметь pct=100 и status="done"
        last = calls[-1]
        self.assertEqual(last[3], 100, "Финальный pct должен быть 100")
        self.assertEqual(last[4], "done")


# ---------------------------------------------------------------------------
# StateStore.get_history_encryption_status
# ---------------------------------------------------------------------------

class TestGetHistoryEncryptionStatus(unittest.TestCase):
    """get_history_encryption_status корректно считает ENC1: vs plaintext."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.data_dir = Path(self._tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_status_on_empty_file(self):
        store = _make_store(self.data_dir)
        status = store.get_history_encryption_status()

        self.assertIn("enabled", status)
        self.assertEqual(status["total"], 0)
        self.assertEqual(status["encrypted"], 0)
        self.assertEqual(status["plaintext"], 0)
        self.assertEqual(status["pct"], 0)

    def test_status_counts_correctly(self):
        store = _make_store(self.data_dir)
        crypto = _make_fake_crypto()

        _write_plaintext_line(self.data_dir / "history.ndjson", _make_item_payload("id1", "Plain"))
        _write_plaintext_line(self.data_dir / "history.ndjson", _make_item_payload("id2", "Plain2"))
        _write_enc1_line(self.data_dir / "history.ndjson", _make_item_payload("id3", "Enc"), crypto)

        status = store.get_history_encryption_status()

        self.assertEqual(status["total"], 3)
        self.assertEqual(status["encrypted"], 1)
        self.assertEqual(status["plaintext"], 2)
        self.assertEqual(status["pct"], 33)  # 1/3 * 100 = 33

    def test_status_all_encrypted(self):
        store = _make_store(self.data_dir)
        crypto = _make_fake_crypto()

        for i in range(4):
            _write_enc1_line(
                self.data_dir / "history.ndjson",
                _make_item_payload(f"id{i}", f"Enc{i}"),
                crypto,
            )

        status = store.get_history_encryption_status()
        self.assertEqual(status["total"], 4)
        self.assertEqual(status["encrypted"], 4)
        self.assertEqual(status["plaintext"], 0)
        self.assertEqual(status["pct"], 100)

    def test_enabled_flag_reflects_settings(self):
        store = _make_store(self.data_dir)
        # По умолчанию выключено
        status = store.get_history_encryption_status()
        self.assertFalse(status["enabled"])

        # Включаем через settings.json
        store.save_settings({"history_encryption_enabled": True})
        status2 = store.get_history_encryption_status()
        self.assertTrue(status2["enabled"])


# ---------------------------------------------------------------------------
# IPC: migrate_history_encryption
# ---------------------------------------------------------------------------

class TestIPCMigrateHistoryEncryption(unittest.TestCase):
    """IPC-обработчик _handle_migrate_history_encryption."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.data_dir = Path(self._tmpdir)
        self.service = None

    def tearDown(self):
        if self.service is not None:
            try:
                self.service.close()
            except Exception:
                pass
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _make_service(self):
        from backend.service import BackendService
        from backend.state_store import StateStore
        store = StateStore(self.data_dir)
        svc = BackendService(store=store)
        self.service = svc
        return svc

    def test_returns_encryption_unavailable_when_keychain_absent(self):
        """На ubuntu CI (нет Keychain) → {"ok": False, "status": "encryption_unavailable"}."""
        self._make_service()
        # Verify the handler correctly returns encryption_unavailable when keychain is absent
        # by patching build_history_crypto to return None (linux CI simulation)
        with patch("backend.history_crypto.build_history_crypto", return_value=None):
            result = self.service._handle_migrate_history_encryption({})
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "encryption_unavailable")

    def test_returns_started_when_crypto_available(self):
        """Когда crypto доступен — возвращает started."""
        svc = self._make_service()
        _write_plaintext_line(self.data_dir / "history.ndjson", _make_item_payload("id1", "T"))

        fake_crypto = _make_fake_crypto()
        # Patch build_history_crypto to return a real HistoryCrypto
        with patch("backend.history_crypto.build_history_crypto", return_value=fake_crypto):
            # Also inject into the store so migration actually works
            svc.store._history_crypto_initialized = True
            svc.store._history_crypto_instance = fake_crypto
            result = svc._handle_migrate_history_encryption({})

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "started")

    def test_already_running_flag(self):
        """Если миграция уже запущена — возвращает already_running."""
        svc = self._make_service()
        svc._history_migration_running = True
        try:
            result = svc._handle_migrate_history_encryption({})
            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "already_running")
        finally:
            svc._history_migration_running = False


# ---------------------------------------------------------------------------
# IPC: get_history_encryption_status
# ---------------------------------------------------------------------------

class TestIPCGetHistoryEncryptionStatus(unittest.TestCase):
    """IPC-обработчик _handle_get_history_encryption_status."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.data_dir = Path(self._tmpdir)
        self.service = None

    def tearDown(self):
        if self.service is not None:
            try:
                self.service.close()
            except Exception:
                pass
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _make_service(self):
        from backend.service import BackendService
        from backend.state_store import StateStore
        store = StateStore(self.data_dir)
        svc = BackendService(store=store)
        self.service = svc
        return svc

    def test_returns_expected_fields(self):
        svc = self._make_service()
        result = svc._handle_get_history_encryption_status({})

        self.assertTrue(result.get("ok"), result)
        self.assertIn("enabled", result)
        self.assertIn("total", result)
        self.assertIn("encrypted", result)
        self.assertIn("plaintext", result)
        self.assertIn("pct", result)
        self.assertIn("migrating", result)

    def test_migrating_false_by_default(self):
        svc = self._make_service()
        result = svc._handle_get_history_encryption_status({})
        self.assertFalse(result["migrating"])

    def test_migrating_true_during_migration(self):
        svc = self._make_service()
        svc._history_migration_running = True
        try:
            result = svc._handle_get_history_encryption_status({})
            self.assertTrue(result["migrating"])
        finally:
            svc._history_migration_running = False

    def test_counts_plaintext_and_encrypted(self):
        svc = self._make_service()
        crypto = _make_fake_crypto()

        _write_plaintext_line(self.data_dir / "history.ndjson", _make_item_payload("id1", "Plain"))
        _write_enc1_line(self.data_dir / "history.ndjson", _make_item_payload("id2", "Enc"), crypto)

        result = svc._handle_get_history_encryption_status({})
        self.assertTrue(result["ok"])
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["encrypted"], 1)
        self.assertEqual(result["plaintext"], 1)


# ---------------------------------------------------------------------------
# Dispatch table sanity
# ---------------------------------------------------------------------------

class TestDispatchTableContainsMigrationHandlers(unittest.TestCase):
    """Оба новых метода зарегистрированы в dispatch table."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.data_dir = Path(self._tmpdir)
        self.service = None

    def tearDown(self):
        if self.service is not None:
            try:
                self.service.close()
            except Exception:
                pass
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_both_methods_in_dispatch_table(self):
        from backend.service import BackendService
        from backend.state_store import StateStore
        store = StateStore(self.data_dir)
        svc = BackendService(store=store)
        self.service = svc

        self.assertIn(
            "migrate_history_encryption",
            svc._dispatch_table,
            "migrate_history_encryption должен быть в dispatch table",
        )
        self.assertIn(
            "get_history_encryption_status",
            svc._dispatch_table,
            "get_history_encryption_status должен быть в dispatch table",
        )


# ---------------------------------------------------------------------------
# DEFAULT_SETTINGS contains history_encryption_enabled
# ---------------------------------------------------------------------------

class TestDefaultSettingsContainsEncryptionFlag(unittest.TestCase):
    """history_encryption_enabled присутствует в DEFAULT_SETTINGS."""

    def test_key_present_and_false(self):
        from core.config import DEFAULT_SETTINGS
        self.assertIn(
            "history_encryption_enabled",
            DEFAULT_SETTINGS,
            "history_encryption_enabled должен быть в DEFAULT_SETTINGS",
        )
        self.assertFalse(
            DEFAULT_SETTINGS["history_encryption_enabled"],
            "По умолчанию шифрование должно быть выключено",
        )


if __name__ == "__main__":
    unittest.main()
