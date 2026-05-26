"""Тесты HMAC-SHA256 хеш-цепочки в PrivacyAuditLogger (W952 F-3 HIGH).

Покрывает:
- test_new_entries_have_prev_hash_chain
- test_verify_chain_detects_tampering
- test_verify_chain_passes_clean_log
- test_legacy_entries_compatible
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT / "KrabEar") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "KrabEar"))

from backend.privacy_audit import (  # noqa: E402
    PrivacyAuditLogger,
    _compute_entry_hash,
    _KEY_FILENAME,
)


class TestHashChainNewEntries(unittest.TestCase):
    """test_new_entries_have_prev_hash_chain — новые записи имеют корректную цепочку."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.log_path = Path(self.tmpdir) / "audit.log"
        PrivacyAuditLogger.reset_instance()
        self.logger = PrivacyAuditLogger(log_path=self.log_path)

    def tearDown(self):
        PrivacyAuditLogger.reset_instance()

    def test_first_entry_has_null_prev_hash(self):
        """Первая запись должна иметь prev_hash=None."""
        self.logger.log_event("sentry", "blocked")
        lines = self.log_path.read_text(encoding="utf-8").strip().splitlines()
        entry = json.loads(lines[0])
        self.assertIn("prev_hash", entry)
        self.assertIsNone(entry["prev_hash"])

    def test_first_entry_has_entry_hash(self):
        """Первая запись должна содержать непустой entry_hash."""
        self.logger.log_event("sentry", "blocked")
        lines = self.log_path.read_text(encoding="utf-8").strip().splitlines()
        entry = json.loads(lines[0])
        self.assertIn("entry_hash", entry)
        self.assertIsInstance(entry["entry_hash"], str)
        self.assertGreater(len(entry["entry_hash"]), 10)

    def test_second_entry_prev_hash_equals_first_entry_hash(self):
        """prev_hash второй записи должен совпадать с entry_hash первой."""
        self.logger.log_event("sentry", "blocked")
        self.logger.log_event("translation", "forced_offline")

        lines = self.log_path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)

        first = json.loads(lines[0])
        second = json.loads(lines[1])

        self.assertEqual(second["prev_hash"], first["entry_hash"])

    def test_chain_is_contiguous_across_five_entries(self):
        """Цепочка непрерывна для 5 записей подряд."""
        for i in range(5):
            self.logger.log_event("cat", "act", {"i": i})

        lines = self.log_path.read_text(encoding="utf-8").strip().splitlines()
        entries = [json.loads(ln) for ln in lines]

        # Первая запись: prev_hash = None
        self.assertIsNone(entries[0]["prev_hash"])

        for idx in range(1, len(entries)):
            self.assertEqual(
                entries[idx]["prev_hash"],
                entries[idx - 1]["entry_hash"],
                msg=f"Обрыв цепочки на индексе {idx}",
            )

    def test_entry_hash_is_hmac_of_body_fields(self):
        """entry_hash рассчитывается от тела записи (без хеш-полей) и prev_hash."""
        self.logger.log_event("sentry", "blocked", {"x": 42})
        lines = self.log_path.read_text(encoding="utf-8").strip().splitlines()
        entry = json.loads(lines[0])

        # Берём ключ из файла
        key_path = self.log_path.parent / _KEY_FILENAME
        secret_key = key_path.read_bytes()

        # Тело без хеш-полей
        body = {k: v for k, v in entry.items() if k not in ("prev_hash", "entry_hash")}
        expected = _compute_entry_hash(secret_key, entry["prev_hash"], body)
        self.assertEqual(expected, entry["entry_hash"])


class TestVerifyChainPassesClean(unittest.TestCase):
    """test_verify_chain_passes_clean_log — чистый лог проходит верификацию."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.log_path = Path(self.tmpdir) / "audit.log"
        PrivacyAuditLogger.reset_instance()
        self.logger = PrivacyAuditLogger(log_path=self.log_path)

    def tearDown(self):
        PrivacyAuditLogger.reset_instance()

    def test_empty_log_is_valid(self):
        """Пустой лог считается валидным."""
        result = self.logger.verify_chain()
        self.assertTrue(result["valid"])
        self.assertIsNone(result["first_broken_index"])
        self.assertEqual(result["checked"], 0)

    def test_single_entry_log_is_valid(self):
        """Лог с одной записью проходит верификацию."""
        self.logger.log_event("sentry", "blocked")
        result = self.logger.verify_chain()
        self.assertTrue(result["valid"])
        self.assertIsNone(result["first_broken_index"])
        self.assertEqual(result["checked"], 1)

    def test_multi_entry_log_is_valid(self):
        """Лог с несколькими записями проходит верификацию."""
        for i in range(10):
            self.logger.log_event("cat", "act", {"n": i})
        result = self.logger.verify_chain()
        self.assertTrue(result["valid"])
        self.assertIsNone(result["first_broken_index"])
        self.assertEqual(result["checked"], 10)

    def test_missing_log_file_is_valid(self):
        """Отсутствующий файл лога считается валидным (нечего проверять)."""
        self.assertFalse(self.log_path.exists())
        result = self.logger.verify_chain()
        self.assertTrue(result["valid"])
        self.assertEqual(result["checked"], 0)


class TestVerifyChainDetectsTampering(unittest.TestCase):
    """test_verify_chain_detects_tampering — подделка обнаруживается."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.log_path = Path(self.tmpdir) / "audit.log"
        PrivacyAuditLogger.reset_instance()
        self.logger = PrivacyAuditLogger(log_path=self.log_path)

    def tearDown(self):
        PrivacyAuditLogger.reset_instance()

    def _write_entries(self, n: int) -> None:
        for i in range(n):
            self.logger.log_event("cat", "act", {"i": i})

    def _read_lines(self) -> list[str]:
        return self.log_path.read_text(encoding="utf-8").strip().splitlines()

    def _write_lines(self, lines: list[str]) -> None:
        self.log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_modified_entry_body_detected(self):
        """Изменение поля action в записи обнаруживается."""
        self._write_entries(3)
        lines = self._read_lines()

        # Подменяем action во второй записи (index 1)
        entry = json.loads(lines[1])
        entry["action"] = "TAMPERED"
        lines[1] = json.dumps(entry, ensure_ascii=False)
        self._write_lines(lines)

        result = self.logger.verify_chain()
        self.assertFalse(result["valid"])
        self.assertEqual(result["first_broken_index"], 1)

    def test_deleted_middle_line_detected(self):
        """Удаление строки посередине обнаруживается (разрыв prev_hash цепочки)."""
        self._write_entries(4)
        lines = self._read_lines()

        # Удаляем вторую запись (index 1)
        del lines[1]
        self._write_lines(lines)

        result = self.logger.verify_chain()
        self.assertFalse(result["valid"])
        # Первый сбой — запись с индексом 1 (бывшая третья, стала второй),
        # чей prev_hash теперь не совпадает
        self.assertIsNotNone(result["first_broken_index"])

    def test_reordered_entries_detected(self):
        """Перестановка двух записей обнаруживается."""
        self._write_entries(3)
        lines = self._read_lines()

        # Меняем первую и вторую записи местами
        lines[0], lines[1] = lines[1], lines[0]
        self._write_lines(lines)

        result = self.logger.verify_chain()
        self.assertFalse(result["valid"])

    def test_tampered_entry_hash_field_detected(self):
        """Прямая подмена поля entry_hash обнаруживается."""
        self._write_entries(2)
        lines = self._read_lines()

        entry = json.loads(lines[0])
        entry["entry_hash"] = "a" * 64  # поддельный hex-digest
        lines[0] = json.dumps(entry, ensure_ascii=False)
        self._write_lines(lines)

        result = self.logger.verify_chain()
        self.assertFalse(result["valid"])
        self.assertEqual(result["first_broken_index"], 0)

    def test_tampered_details_detected(self):
        """Изменение поля details обнаруживается."""
        self._write_entries(5)
        lines = self._read_lines()

        # Подменяем details в третьей записи (index 2)
        entry = json.loads(lines[2])
        entry["details"]["i"] = 9999
        lines[2] = json.dumps(entry, ensure_ascii=False)
        self._write_lines(lines)

        result = self.logger.verify_chain()
        self.assertFalse(result["valid"])
        self.assertEqual(result["first_broken_index"], 2)


class TestLegacyEntriesCompatible(unittest.TestCase):
    """test_legacy_entries_compatible — старые записи без хешей не ломают цепочку."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.log_path = Path(self.tmpdir) / "audit.log"
        PrivacyAuditLogger.reset_instance()

    def tearDown(self):
        PrivacyAuditLogger.reset_instance()

    def _make_legacy_entry(self, category: str, action: str) -> str:
        """Создаёт строку записи в старом формате (без prev_hash/entry_hash)."""
        from datetime import datetime, timezone
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "category": category,
            "action": action,
            "details": {},
        }
        return json.dumps(entry, ensure_ascii=False)

    def test_log_with_only_legacy_entries_is_valid(self):
        """Лог только из legacy-записей считается валидным (нечего проверять)."""
        lines = [
            self._make_legacy_entry("sentry", "blocked"),
            self._make_legacy_entry("translation", "forced_offline"),
        ]
        self.log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        logger = PrivacyAuditLogger(log_path=self.log_path)
        result = logger.verify_chain()
        self.assertTrue(result["valid"])
        # checked == 2 (legacy записи считаются)
        self.assertEqual(result["checked"], 2)

    def test_new_entries_after_legacy_start_fresh_chain(self):
        """После legacy-записей новые записи образуют цепочку с prev_hash=None."""
        # Записываем legacy-записи вручную
        legacy_line = self._make_legacy_entry("sentry", "blocked")
        self.log_path.write_text(legacy_line + "\n", encoding="utf-8")

        # Создаём logger — он прочитает legacy-запись и установит _last_hash = None
        logger = PrivacyAuditLogger(log_path=self.log_path)
        logger.log_event("translation", "forced_offline")

        lines = self.log_path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)

        new_entry = json.loads(lines[1])
        self.assertIn("prev_hash", new_entry)
        self.assertIsNone(new_entry["prev_hash"])  # перезапуск цепочки
        self.assertIn("entry_hash", new_entry)

    def test_verify_chain_passes_mixed_legacy_and_new(self):
        """Лог из mix legacy + новых записей проходит verify_chain."""
        # Записываем 2 legacy-записи
        lines = [
            self._make_legacy_entry("sentry", "blocked"),
            self._make_legacy_entry("translation", "forced_offline"),
        ]
        self.log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        # Добавляем 3 новых записи через logger
        logger = PrivacyAuditLogger(log_path=self.log_path)
        for i in range(3):
            logger.log_event("cat", "act", {"i": i})

        result = logger.verify_chain()
        self.assertTrue(result["valid"])
        # 2 legacy + 3 новых = 5 checked
        self.assertEqual(result["checked"], 5)

    def test_tampering_new_entry_after_legacy_detected(self):
        """Подделка новой записи после legacy-записей обнаруживается."""
        # Записываем 1 legacy-запись
        legacy_line = self._make_legacy_entry("sentry", "blocked")
        self.log_path.write_text(legacy_line + "\n", encoding="utf-8")

        logger = PrivacyAuditLogger(log_path=self.log_path)
        logger.log_event("cat", "act", {"i": 0})
        logger.log_event("cat", "act", {"i": 1})

        # Читаем и подделываем вторую новую запись (index 2)
        all_lines = self.log_path.read_text(encoding="utf-8").strip().splitlines()
        entry = json.loads(all_lines[2])
        entry["action"] = "TAMPERED"
        all_lines[2] = json.dumps(entry, ensure_ascii=False)
        self.log_path.write_text("\n".join(all_lines) + "\n", encoding="utf-8")

        # Сбрасываем singleton чтобы verify_chain перечитал файл
        PrivacyAuditLogger.reset_instance()
        fresh_logger = PrivacyAuditLogger(log_path=self.log_path)
        result = fresh_logger.verify_chain()
        self.assertFalse(result["valid"])
        self.assertEqual(result["first_broken_index"], 2)


if __name__ == "__main__":
    unittest.main()
