"""Illegible shares_index.json must fail CLOSED, never be overwritten by a near-empty index.

Read-modify-write wipe class: ``_load_index`` swallowed a parse/read failure and left
``_index = {}``; the next ``prepare_share`` / ``revoke_share`` then rewrote the whole
file, silently dropping every live share (their payload files stayed on disk with no
index entry — un-revocable plaintext) and every revocation tombstone.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from _plaintext_export_test_helpers import off_authorizer

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.ipc_errors import IpcOperationalError  # noqa: E402
from backend.sharing_manager import SharePersistenceError, SharingManager  # noqa: E402


class _Item:
    def __init__(self, item_id: str, text: str) -> None:
        self.id = item_id
        self.text = text
        self.ts = "2024-01-01T10:00:00+00:00"
        self.translated_text = ""
        self.source_lang = "ru"
        self.target_lang = "es"

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "text": self.text, "ts": self.ts, "translated_text": "",
                "source_lang": "ru", "target_lang": "es"}


class _Store:
    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        self.items = {"i1": _Item("i1", "секретный текст")}

    def get_history_item_by_id(self, item_id: str):
        return self.items.get(item_id)


class SharingIndexFailClosedTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.store = _Store(self.dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _mgr(self) -> SharingManager:
        return SharingManager(store=self.store, share_no_default_ttl=True,
                              plaintext_export_authorizer=off_authorizer())

    def _index_path(self) -> Path:
        return Path(self.dir) / "shares" / "shares_index.json"

    def _seed_valid_share(self) -> str:
        pkg = self._mgr().prepare_share(["i1"], format="text")
        return pkg.share_id

    def _corrupt(self, raw: bytes = b'{"abc": {"share_id": "abc", ') -> bytes:
        self._index_path().write_bytes(raw)
        return raw

    def test_prepare_share_refuses_and_preserves_unreadable_index(self) -> None:
        self._seed_valid_share()
        raw = self._corrupt()
        mgr = self._mgr()
        files_before = sorted(p.name for p in (Path(self.dir) / "shares").iterdir())
        with self.assertRaises(SharePersistenceError):
            mgr.prepare_share(["i1"], format="text")
        self.assertEqual(self._index_path().read_bytes(), raw)
        # No new orphan payload file is written when the index cannot be updated.
        self.assertEqual(sorted(p.name for p in (Path(self.dir) / "shares").iterdir()), files_before)

    def test_non_dict_index_is_treated_as_unreadable(self) -> None:
        self._seed_valid_share()
        raw = self._corrupt(b"[1, 2, 3]")
        mgr = self._mgr()
        with self.assertRaises(SharePersistenceError):
            mgr.prepare_share(["i1"], format="text")
        self.assertEqual(self._index_path().read_bytes(), raw)

    def test_revoke_on_unreadable_index_raises_instead_of_reporting_not_found(self) -> None:
        share_id = self._seed_valid_share()
        raw = self._corrupt()
        mgr = self._mgr()
        with self.assertRaises(IpcOperationalError):
            mgr.revoke_share(share_id)
        self.assertEqual(self._index_path().read_bytes(), raw)

    def test_reads_do_not_report_anything_valid_when_unreadable(self) -> None:
        share_id = self._seed_valid_share()
        self._corrupt()
        mgr = self._mgr()
        self.assertIsNone(mgr.get_shared(share_id))
        self.assertEqual(mgr.list_shared(include_expired=True, include_revoked=True), [])

    def test_missing_index_is_a_normal_fresh_start(self) -> None:
        mgr = self._mgr()
        pkg = mgr.prepare_share(["i1"], format="text")
        self.assertIsNotNone(mgr.get_shared(pkg.share_id))

    def test_repaired_index_recovers_and_keeps_old_entries(self) -> None:
        share_id = self._seed_valid_share()
        good = self._index_path().read_bytes()
        self._corrupt()
        mgr = self._mgr()
        with self.assertRaises(SharePersistenceError):
            mgr.prepare_share(["i1"], format="text")
        self._index_path().write_bytes(good)  # operator restores the file
        pkg2 = mgr.prepare_share(["i1"], format="text")
        index = json.loads(self._index_path().read_text(encoding="utf-8"))
        self.assertIn(share_id, index)
        self.assertIn(pkg2.share_id, index)
        self.assertIsNotNone(mgr.get_shared(share_id))

    def test_purge_all_resets_unreadable_state(self) -> None:
        self._seed_valid_share()
        self._corrupt()
        mgr = self._mgr()
        mgr.purge_all()
        pkg = mgr.prepare_share(["i1"], format="text")
        self.assertIsNotNone(mgr.get_shared(pkg.share_id))


if __name__ == "__main__":
    unittest.main()
