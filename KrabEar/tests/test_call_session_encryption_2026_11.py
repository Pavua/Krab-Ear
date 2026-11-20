"""Шифрование call_sessions.ndjson при history_encryption_enabled.

Журнал звонков хранил номер, цель и реплики открытым текстом даже при
включённом шифровании истории: ``HISTORY_JOURNAL_FILENAMES`` его не знал.
Решение: ``CallSessionStore`` пишет строки как ``ENC1:`` тем же ``HistoryCrypto``,
читает оба формата, не затрагивает реестр из десяти журналов (инвариант
снапшотов), без ключа отказывает в записи вместо молчаливого plaintext.
"""
from __future__ import annotations

import ast
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.call_session_store import (  # noqa: E402
    CallSessionEncryptionUnavailable,
    CallSessionStore,
)
from backend.history_crypto import SENTINEL, HistoryCrypto  # noqa: E402

PHONE = "+34911234567"
GOAL = "уточнить результаты анализов в клинике"
SPEECH = "пациентка принимает сертралин"
BACKEND = PROJECT_ROOT / "backend"


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.crypto = HistoryCrypto(os.urandom(32))

    def store(self, *, on: bool, crypto="default") -> CallSessionStore:
        provider = self.crypto if crypto == "default" else crypto
        return CallSessionStore(
            self.dir,
            policy_read=lambda: on,
            crypto_getter=lambda: provider,
        )

    @property
    def raw(self) -> str:
        return (self.dir / "call_sessions.ndjson").read_text(encoding="utf-8")

    def lines(self) -> list[str]:
        return [ln for ln in self.raw.splitlines() if ln.strip()]


class OffProfileUnchangedTest(_Base):
    def test_off_writes_plain_json_and_roundtrips(self) -> None:
        s = self.store(on=False)
        sess = s.create(PHONE, GOAL)
        s.add_transcript(sess.id, "agent", SPEECH)

        self.assertIn(PHONE, self.raw)
        self.assertFalse(any(ln.startswith(SENTINEL) for ln in self.lines()))
        got = s.get(sess.id)
        self.assertEqual(got.goal_text, GOAL)
        self.assertEqual(got.transcript_history[0].text, SPEECH)


class OnProfileEncryptsTest(_Base):
    def test_on_file_has_no_plaintext_pii(self) -> None:
        s = self.store(on=True)
        sess = s.create(PHONE, GOAL)
        for status in ("dialing", "connected", "ending"):
            s.update_status(sess.id, status)
        s.add_transcript(sess.id, "agent", SPEECH)
        s.mark_completed(sess.id, "completed", cost_usd=0.5)

        for needle in (PHONE, GOAL, SPEECH, "анализов", "сертралин"):
            self.assertNotIn(needle, self.raw)
        self.assertTrue(self.lines())
        self.assertTrue(all(ln.startswith(SENTINEL) for ln in self.lines()))

    def test_on_roundtrip_full_lifecycle(self) -> None:
        s = self.store(on=True)
        sess = s.create(PHONE, GOAL)
        for status in ("dialing", "connected", "ending"):
            s.update_status(sess.id, status)
        s.add_transcript(sess.id, "agent", SPEECH)
        s.mark_completed(sess.id, "completed", cost_usd=0.5)

        fresh = self.store(on=True)
        got = fresh.get(sess.id)
        self.assertEqual(got.phone_number, PHONE)
        self.assertEqual(got.goal_text, GOAL)
        self.assertEqual(got.status, "completed")
        self.assertEqual(got.transcript_history[0].text, SPEECH)
        self.assertEqual(got.cost_usd, 0.5)
        self.assertEqual([x["id"] for x in fresh.list_sessions()], [sess.id])

    def test_on_without_key_refuses_write_and_leaves_no_plaintext(self) -> None:
        s = self.store(on=True, crypto=None)

        with self.assertRaises(CallSessionEncryptionUnavailable):
            s.create(PHONE, GOAL)

        self.assertEqual(self.raw, "")

    def test_unreadable_policy_is_treated_as_on(self) -> None:
        def broken() -> bool:
            raise OSError("settings нечитаем")

        s = CallSessionStore(self.dir, policy_read=broken, crypto_getter=lambda: self.crypto)
        s.create(PHONE, GOAL)

        self.assertNotIn(PHONE, self.raw)
        self.assertTrue(all(ln.startswith(SENTINEL) for ln in self.lines()))

    def test_mixed_legacy_plain_and_encrypted_lines_are_readable(self) -> None:
        legacy = self.store(on=False)
        old = legacy.create(PHONE, GOAL)
        enc = self.store(on=True)
        new = enc.create("+34911000000", "другая цель")

        ids = {x["id"] for x in enc.list_sessions()}
        self.assertEqual(ids, {old.id, new.id})

    def test_delete_all_works_when_encrypted(self) -> None:
        s = self.store(on=True)
        s.create(PHONE, GOAL)
        s.create("+34911000001", "x")

        self.assertEqual(s.delete_all(), 2)
        self.assertEqual(self.raw, "")


class UndecryptableDataIsNeverDroppedTest(_Base):
    def test_lost_key_hides_sessions_but_compact_refuses_to_rewrite(self) -> None:
        s = self.store(on=True)
        s.create(PHONE, GOAL)
        before = self.raw

        keyless = self.store(on=True, crypto=None)
        self.assertEqual(keyless.list_sessions(), [])
        stats = keyless.compact()

        self.assertEqual(self.raw, before, "compact затёр зашифрованные строки без ключа")
        self.assertGreater(stats.get("skipped_undecryptable", 0), 0)

    def test_wrong_key_does_not_crash_and_does_not_rewrite(self) -> None:
        self.store(on=True).create(PHONE, GOAL)
        before = self.raw

        other = self.store(on=True, crypto=HistoryCrypto(os.urandom(32)))
        self.assertEqual(other.list_sessions(), [])
        other.compact()

        self.assertEqual(self.raw, before)


class CompactEncryptsTest(_Base):
    def test_compact_rewrites_legacy_plaintext_as_encrypted_when_on(self) -> None:
        legacy = self.store(on=False)
        sess = legacy.create(PHONE, GOAL)
        legacy.add_transcript(sess.id, "agent", SPEECH)

        on = self.store(on=True)
        on.compact()

        self.assertNotIn(PHONE, self.raw)
        self.assertTrue(all(ln.startswith(SENTINEL) for ln in self.lines()))
        self.assertEqual(on.get(sess.id).transcript_history[0].text, SPEECH)


class MigrationTest(_Base):
    def _legacy(self) -> str:
        legacy = self.store(on=False)
        sess = legacy.create(PHONE, GOAL)
        legacy.update_status(sess.id, "dialing")
        legacy.add_transcript(sess.id, "agent", SPEECH)
        return sess.id

    def test_migrate_encrypts_every_plaintext_line(self) -> None:
        sid = self._legacy()
        on = self.store(on=True)

        result = on.migrate_encryption()

        self.assertTrue(result["ok"], result)
        self.assertGreaterEqual(result["encrypted"], 3)
        for needle in (PHONE, GOAL, SPEECH):
            self.assertNotIn(needle, self.raw)
        self.assertTrue(all(ln.startswith(SENTINEL) for ln in self.lines()))
        got = on.get(sid)
        self.assertEqual((got.phone_number, got.goal_text), (PHONE, GOAL))
        self.assertEqual(got.transcript_history[0].text, SPEECH)

    def test_migrate_leaves_no_plaintext_sidecars(self) -> None:
        self._legacy()
        self.store(on=True).migrate_encryption()

        leftovers = sorted(p.name for p in self.dir.iterdir()
                           if p.name.startswith("call_sessions") and p.suffix not in {".ndjson", ".lock"})
        self.assertEqual(leftovers, [])

    def test_migrate_is_idempotent(self) -> None:
        self._legacy()
        on = self.store(on=True)
        on.migrate_encryption()
        snapshot = self.lines()

        again = on.migrate_encryption()

        self.assertTrue(again["ok"])
        self.assertEqual(again["encrypted"], 0)
        self.assertEqual(self.lines(), snapshot)

    def test_migrate_when_policy_off_is_a_noop(self) -> None:
        self._legacy()
        before = self.raw

        result = self.store(on=False).migrate_encryption()

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "encryption_disabled")
        self.assertEqual(self.raw, before)

    def test_migrate_without_key_is_unavailable_and_keeps_file(self) -> None:
        self._legacy()
        before = self.raw

        result = self.store(on=True, crypto=None).migrate_encryption()

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "encryption_unavailable")
        self.assertEqual(self.raw, before)

    def test_failed_verification_keeps_original_and_cleans_tmp(self) -> None:
        self._legacy()
        before = self.raw

        class _BrokenDecrypt(HistoryCrypto):
            def decrypt_line(self, token: str) -> str:
                raise ValueError("не расшифровывается")

        broken = _BrokenDecrypt(os.urandom(32))
        result = self.store(on=True, crypto=broken).migrate_encryption()

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "verification_failed")
        self.assertEqual(self.raw, before)
        leftovers = sorted(p.name for p in self.dir.iterdir()
                           if p.name.startswith("call_sessions") and p.suffix not in {".ndjson", ".lock"})
        self.assertEqual(leftovers, [])


class DefaultWiringTest(_Base):
    def test_default_policy_reads_settings_json(self) -> None:
        (self.dir / "settings.json").write_text(
            json.dumps({"history_encryption_enabled": False, "privacy_mode_enabled": False}),
            encoding="utf-8",
        )

        s = CallSessionStore(self.dir)  # без инъекций: путь прод-кода по умолчанию
        s.create(PHONE, GOAL)

        self.assertIn(PHONE, self.raw)


class ServiceWiringTest(unittest.TestCase):
    """Декоративная проводка: хранилище создано, но боевые зависимости не переданы."""

    def _calls(self, name: str) -> list[ast.Call]:
        tree = ast.parse((BACKEND / "service.py").read_text(encoding="utf-8"))
        return [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and (getattr(n.func, "id", None) == name or getattr(n.func, "attr", None) == name)
        ]

    def test_service_passes_store_policy_and_crypto_to_call_store(self) -> None:
        calls = self._calls("CallSessionStore")
        self.assertTrue(calls)
        kw = {k.arg for c in calls for k in c.keywords}
        self.assertIn("policy_read", kw)
        self.assertIn("crypto_getter", kw)

    def test_service_runs_call_session_migration(self) -> None:
        self.assertTrue(
            self._calls("migrate_encryption"),
            "service.py обязан запускать migrate_encryption() журнала звонков",
        )


if __name__ == "__main__":
    unittest.main()
