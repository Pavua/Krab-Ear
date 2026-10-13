"""Нечитаемый журнал не должен валить старт, purge и запись настроек.

Ревью ветки нашло три регрессии от fail-closed чтения журналов:
 1. maybe_compact() на старте падал на нечитаемом history_status.ndjson (crash loop);
 2. purge_all_data падал до любого wipe при нечитаемом побочном журнале;
 3. has_encrypted_history_in(нечитаем)=True блокировал ЛЮБУЮ запись настроек при OFF.
Плюс сиблинг: не-объектный settings.json / null-флаг privacy открывали privacy-гейты.
Fail-closed остаётся там, где он решает судьбу данных (компактация, выбор plaintext vs ENC1).
"""

from __future__ import annotations

import errno
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend import state_store as ss  # noqa: E402
from backend.history_service import HistoryService  # noqa: E402
from backend.plaintext_export_authorization import POLICY_REVISION_KEY  # noqa: E402
from backend.state_store import StateStore  # noqa: E402


def _deny_open(store: StateStore, target: Path):
    real_open = os.open
    name = target.name

    def fake(path, flags, *a, **kw):
        if Path(path).name == name:
            raise OSError(errno.EIO, "io")
        return real_open(path, flags, *a, **kw)

    return patch.object(ss.os, "open", side_effect=fake)


class DegradeOnUnreadableJournalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        self.store.add_history_item(text="alpha")

    def test_maybe_compact_does_not_raise_on_unreadable_status_journal(self) -> None:
        with _deny_open(self.store, self.store.status_path):
            self.assertFalse(self.store.maybe_compact())

    def test_explicit_compact_still_fails_loudly(self) -> None:
        with _deny_open(self.store, self.store.history_path):
            with self.assertRaises(OSError):
                self.store.compact()

    def test_purge_continues_when_side_journal_unreadable(self) -> None:
        svc = HistoryService(store=self.store)
        with _deny_open(self.store, self.store.tags_path):
            result = svc.handle_purge_all_data({"confirm": True})
        self.assertIsInstance(result, dict)
        self.assertNotEqual(result.get("error"), "internal_error")

    def test_settings_write_not_blocked_by_unreadable_journal_when_off(self) -> None:
        self.store.settings_path.write_text(
            json.dumps({
                "history_encryption_enabled": False,
                "privacy_mode_enabled": False,
                POLICY_REVISION_KEY: "0123456789abcdef0123456789abcdef",
            }),
            encoding="utf-8",
        )
        real_open = Path.open
        journal = self.store.tags_path
        journal.write_text('{"id": "x"}\n', encoding="utf-8")

        def deny(self_path, *a, **kw):
            if self_path == journal:
                raise PermissionError(errno.EACCES, "denied")
            return real_open(self_path, *a, **kw)

        with patch.object(Path, "open", deny):
            self.store.save_settings({"privacy_mode_enabled": True})
        self.assertTrue(self.store.load_settings()["privacy_mode_enabled"])

    def test_non_object_settings_forces_privacy_on(self) -> None:
        for text in ("[]", "1", '"x"', "null"):
            self.store.settings_path.write_text(text, encoding="utf-8")
            self.assertTrue(self.store.load_settings()["privacy_mode_enabled"], text)

    def test_non_bool_privacy_value_forces_privacy_on(self) -> None:
        for value in (None, 0, "false"):
            self.store.settings_path.write_text(
                json.dumps({"privacy_mode_enabled": value}), encoding="utf-8"
            )
            self.assertTrue(self.store.load_settings()["privacy_mode_enabled"], repr(value))

    def test_explicit_privacy_off_still_honoured(self) -> None:
        self.store.settings_path.write_text(
            json.dumps({"privacy_mode_enabled": False}), encoding="utf-8"
        )
        self.assertFalse(self.store.load_settings()["privacy_mode_enabled"])


if __name__ == "__main__":
    unittest.main()
