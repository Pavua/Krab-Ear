"""Внешнее ревью E2 (2026-11-15): дыры в целостности журнала приватности.

1. ``verify_chain`` принимал запись БЕЗ ``entry_hash``/``prev_hash`` как «легаси»
   в ЛЮБОМ месте журнала. Кто может писать в файл, но не знает HMAC-ключа,
   стирал хеш-поля и подделывал тело записи; удаление головы журнала тоже
   проходило как ``valid=True``.
2. ``_load_or_create_key`` перезаписывал рабочий ключ при ЛЮБОЙ ошибке чтения,
   включая временный ``OSError``: все прежние записи становились невалидными.

Легаси-ПРЕФИКС (старые записи до первой хешированной) остаётся валидным: именно
так выглядит журнал после обновления с версии без хеш-цепочки.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT / "KrabEar") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "KrabEar"))

from backend.privacy_audit import (  # noqa: E402
    _KEY_FILENAME,
    PrivacyAuditLogger,
    _load_or_create_key,
)


def _legacy_line(category: str = "sentry", action: str = "blocked") -> str:
    return json.dumps(
        {"ts": "2026-01-01T00:00:00+00:00", "category": category, "action": action},
        sort_keys=True,
    )


class _ChainBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.log_path = Path(self.tmpdir) / "audit.log"
        PrivacyAuditLogger.reset_instance()
        self.audit = PrivacyAuditLogger(log_path=self.log_path)

    def tearDown(self):
        PrivacyAuditLogger.reset_instance()

    def _lines(self) -> list[str]:
        return self.log_path.read_text(encoding="utf-8").splitlines()

    def _write(self, lines: list[str]) -> None:
        self.log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _three_hashed(self) -> None:
        self.audit.log_event("sentry", "blocked")
        self.audit.log_event("translation", "forced_offline")
        self.audit.log_event("rewriter", "skipped")
        self.assertTrue(self.audit.verify_chain()["valid"])


class VerifyChainRejectsStrippedEntries(_ChainBase):
    def test_hash_fields_stripped_from_middle_entry_is_broken(self):
        self._three_hashed()
        lines = self._lines()
        mid = json.loads(lines[1])
        mid.pop("entry_hash")
        mid.pop("prev_hash")
        mid["action"] = "forged"  # подделка тела без ключа
        lines[1] = json.dumps(mid, sort_keys=True)
        self._write(lines)

        res = self.audit.verify_chain()

        self.assertFalse(res["valid"])
        self.assertEqual(res["first_broken_index"], 1)

    def test_legacy_line_injected_after_hashed_entry_is_broken(self):
        self._three_hashed()
        lines = self._lines()
        lines.insert(2, _legacy_line())
        self._write(lines)

        res = self.audit.verify_chain()

        self.assertFalse(res["valid"])
        self.assertEqual(res["first_broken_index"], 2)

    def test_legacy_line_appended_at_tail_is_broken(self):
        self._three_hashed()
        self._write(self._lines() + [_legacy_line()])

        self.assertFalse(self.audit.verify_chain()["valid"])

    def test_deleting_head_entry_is_broken(self):
        self._three_hashed()
        self._write(self._lines()[1:])  # снесли голову журнала

        res = self.audit.verify_chain()

        self.assertFalse(res["valid"])
        self.assertEqual(res["first_broken_index"], 0)


class VerifyChainKeepsLegacyPrefixValid(_ChainBase):
    """Охрана от перегиба: апгрейдный журнал не должен краснеть."""

    def test_legacy_prefix_then_hashed_entries_is_valid(self):
        self._write([_legacy_line("a", "x"), _legacy_line("b", "y")])
        PrivacyAuditLogger.reset_instance()
        audit = PrivacyAuditLogger(log_path=self.log_path)
        audit.log_event("sentry", "blocked")
        audit.log_event("translation", "forced_offline")

        res = audit.verify_chain()

        self.assertTrue(res["valid"], res)
        self.assertEqual(res["checked"], 4)

    def test_clear_then_new_entries_start_valid_chain(self):
        """Единственный путь, убирающий начало журнала — ``clear()`` (удаляет
        файл целиком): новая цепочка начинается с prev_hash=None и валидна.
        Ротации/обрезки журнала по размеру в коде нет."""
        self._three_hashed()
        self.audit.clear()
        self.audit.log_event("sentry", "blocked")
        self.audit.log_event("translation", "forced_offline")

        res = self.audit.verify_chain()

        self.assertTrue(res["valid"], res)
        self.assertEqual(res["checked"], 2)
        first = json.loads(self._lines()[0])
        self.assertIsNone(first["prev_hash"])

    def test_second_process_instance_continues_chain_validly(self):
        """Новый экземпляр (рестарт процесса) продолжает цепочку по хвосту файла."""
        self._three_hashed()
        PrivacyAuditLogger.reset_instance()
        other = PrivacyAuditLogger(log_path=self.log_path)
        other.log_event("rewriter", "skipped")

        res = other.verify_chain()

        self.assertTrue(res["valid"], res)
        self.assertEqual(res["checked"], 4)

    def test_only_legacy_entries_is_valid(self):
        self._write([_legacy_line("a", "x"), _legacy_line("b", "y")])

        self.assertTrue(self.audit.verify_chain()["valid"])


class KeyIsNotOverwrittenOnTransientReadError(unittest.TestCase):
    def setUp(self):
        self.data_dir = Path(tempfile.mkdtemp())
        self.key_path = self.data_dir / _KEY_FILENAME

    def test_oserror_on_read_keeps_working_key_and_raises(self):
        key = _load_or_create_key(self.data_dir)
        self.assertEqual(len(key), 32)
        original_read_bytes = Path.read_bytes
        key_path = self.key_path

        def flaky(self_path: Path):
            if self_path == key_path:
                raise PermissionError("временный отказ")
            return original_read_bytes(self_path)

        with mock.patch.object(Path, "read_bytes", flaky):
            with self.assertRaises(OSError):
                _load_or_create_key(self.data_dir)

        # Ключ на диске не тронут: следующее чтение отдаёт ТОТ ЖЕ ключ.
        self.assertEqual(self.key_path.read_bytes(), key)
        self.assertEqual(_load_or_create_key(self.data_dir), key)

    def test_wrong_length_key_is_still_regenerated(self):
        self.key_path.write_bytes(b"short")

        key = _load_or_create_key(self.data_dir)

        self.assertEqual(len(key), 32)
        self.assertEqual(self.key_path.read_bytes(), key)

    def test_missing_key_is_created(self):
        key = _load_or_create_key(self.data_dir)

        self.assertEqual(len(key), 32)
        self.assertTrue(self.key_path.exists())


if __name__ == "__main__":
    unittest.main()
