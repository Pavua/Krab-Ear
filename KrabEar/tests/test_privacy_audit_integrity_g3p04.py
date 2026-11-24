"""G3-privacy-04: целостность ключа и проверки HMAC-цепочки журнала приватности.

P04-01 — ключ неверной длины не затирается молча (карантин + событие в журнале),
сбой записи нового ключа не даёт эфемерного ключа, гонка двух создателей не
перетирает ключ победителя.
P04-02 — verify_chain не пропускает нечитаемые строки внутри цепочки, не падает
на нестроковых хеш-полях и не пишет сырые строки журнала в лог; писатель не
склеивает новую запись с оборванным хвостом.
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

from backend import privacy_audit  # noqa: E402
from backend.privacy_audit import _KEY_FILENAME, PrivacyAuditLogger  # noqa: E402

SECRET_MARKER = "SECRET-LINE-MARKER-7f3a"


class _Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.log_path = self.dir / "audit.log"
        self.key_path = self.dir / _KEY_FILENAME
        PrivacyAuditLogger.reset_instance()
        self.addCleanup(PrivacyAuditLogger.reset_instance)

    def entries(self):
        return [json.loads(x) for x in self.log_path.read_text("utf-8").splitlines() if x.strip()]


class KeyIntegrityTests(_Base):
    def test_wrong_length_key_is_quarantined_not_destroyed(self):
        bad = b"short-key-16byte"
        self.key_path.write_bytes(bad)
        with self.assertLogs("KrabEar.Backend.PrivacyAudit", level="ERROR"):
            audit = PrivacyAuditLogger(log_path=self.log_path)
        quarantined = sorted(self.dir.glob(_KEY_FILENAME + ".invalid-*"))
        self.assertEqual(len(quarantined), 1, "старый ключ обязан сохраниться для разбора")
        self.assertEqual(quarantined[0].read_bytes(), bad)
        self.assertEqual(len(self.key_path.read_bytes()), 32)
        self.assertEqual(audit._secret_key, self.key_path.read_bytes())
        events = [e for e in self.entries() if e.get("action") == "key_quarantined"]
        self.assertEqual(len(events), 1, "ротация ключа обязана быть видна в самом журнале")
        self.assertEqual(events[0]["details"]["key_length"], len(bad))
        self.assertEqual(events[0]["details"]["quarantined_as"], quarantined[0].name)

    def test_key_write_failure_raises_instead_of_ephemeral_key(self):
        with mock.patch.object(privacy_audit.tempfile, "mkstemp", side_effect=OSError("disk full")):
            with self.assertRaises(privacy_audit.AuditKeyUnavailable):
                PrivacyAuditLogger(log_path=self.log_path)
        self.assertFalse(self.key_path.exists())

    def test_concurrent_creator_key_is_not_overwritten(self):
        winner = bytes(range(32))
        real_mkstemp = privacy_audit.tempfile.mkstemp

        def racing_mkstemp(*args, **kwargs):
            # Соседний процесс успел опубликовать свой ключ между проверкой и записью.
            if not self.key_path.exists():
                self.key_path.write_bytes(winner)
            return real_mkstemp(*args, **kwargs)

        with mock.patch.object(privacy_audit.tempfile, "mkstemp", side_effect=racing_mkstemp):
            audit = PrivacyAuditLogger(log_path=self.log_path)
        self.assertEqual(self.key_path.read_bytes(), winner)
        self.assertEqual(audit._secret_key, winner)
        self.assertEqual(list(self.dir.glob(".privacy_audit.key.*")), [], "временный файл не убран")

    def test_short_os_write_still_persists_full_key(self):
        real_write = privacy_audit.os.write

        def short_write(fd, data):
            return real_write(fd, bytes(data[:5]))  # ядро вправе записать меньше

        with mock.patch.object(privacy_audit.os, "write", side_effect=short_write):
            audit = PrivacyAuditLogger(log_path=self.log_path)
        self.assertEqual(len(self.key_path.read_bytes()), 32)
        self.assertEqual(audit._secret_key, self.key_path.read_bytes())

    def test_existing_valid_key_is_reused(self):
        key = bytes(range(100, 132))
        self.key_path.write_bytes(key)
        audit = PrivacyAuditLogger(log_path=self.log_path)
        self.assertEqual(audit._secret_key, key)
        self.assertFalse(self.log_path.exists(), "чистый старт не пишет служебных событий")


class VerifyChainTests(_Base):
    def setUp(self):
        super().setUp()
        self.audit = PrivacyAuditLogger(log_path=self.log_path)
        for i in range(3):
            self.audit.log_event("test", f"a{i}", {"i": i})

    def _lines(self):
        return self.log_path.read_text("utf-8").splitlines()

    def _write(self, lines):
        self.log_path.write_text("\n".join(lines) + "\n", "utf-8")

    def test_inserted_garbage_line_breaks_chain(self):
        lines = self._lines()
        lines.insert(2, "{not json " + SECRET_MARKER)
        self._write(lines)
        result = self.audit.verify_chain()
        self.assertFalse(result["valid"])
        self.assertEqual(result["first_broken_index"], 2)
        self.assertEqual(result["reason"], "unparsable_line")

    def test_garbage_in_legacy_prefix_is_tolerated(self):
        lines = ["{legacy torn", json.dumps({"ts": "x", "category": "c", "action": "old"})] + self._lines()
        self._write(lines)
        self.assertTrue(self.audit.verify_chain()["valid"])

    def test_non_string_hash_fields_are_invalid_not_crash(self):
        lines = self._lines()
        entry = json.loads(lines[1])
        entry["entry_hash"] = 12345
        lines[1] = json.dumps(entry)
        self._write(lines)
        result = self.audit.verify_chain()
        self.assertFalse(result["valid"])
        self.assertEqual(result["first_broken_index"], 1)

    def test_valid_chain_reports_reason_none(self):
        result = self.audit.verify_chain()
        self.assertEqual(result, {"valid": True, "first_broken_index": None, "checked": 3, "reason": None})

    def test_raw_line_content_never_logged(self):
        lines = self._lines()
        lines.insert(1, "{broken " + SECRET_MARKER)
        self._write(lines)
        with self.assertLogs("KrabEar.Backend.PrivacyAudit", level="WARNING") as captured:
            self.audit.verify_chain()
            self.audit.read_entries()
            self.audit.summarize()
        text = "\n".join(captured.output)
        self.assertNotIn(SECRET_MARKER, text)
        self.assertIn("строка 2", text)

    def test_torn_tail_is_not_glued_to_next_entry(self):
        with self.log_path.open("ab") as fh:
            fh.write(b'{"ts": "torn')  # обрыв записи без перевода строки
        self.audit.log_event("test", "after_crash", {})
        last = self._lines()[-1]
        self.assertEqual(json.loads(last)["action"], "after_crash")
        result = self.audit.verify_chain()
        self.assertFalse(result["valid"], "оборванная запись внутри цепочки — улика, не норма")
        self.assertEqual(result["reason"], "unparsable_line")
        self.assertEqual(result["first_broken_index"], 3)


if __name__ == "__main__":
    unittest.main()
